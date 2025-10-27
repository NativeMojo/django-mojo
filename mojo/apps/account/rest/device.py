from mojo import decorators as md
from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
from mojo.apps.account.models.geolocated_ip import GeoLocatedIP


@md.URL('user/device')
@md.URL('user/device/<int:pk>')
def on_user_device(request, pk=None):
    return UserDevice.on_rest_request(request, pk)


@md.GET('user/device/lookup')
@md.requires_params('duid')
@md.requires_perms("manage_users", "manage_devices")
def on_user_device_by_duid(request):
    duid = request.DATA.get('duid')
    device = UserDevice.objects.filter(duid=duid).first()
    if not device:
        return UserDevice.rest_error_response(request, 404, error="Device not found")
    return device.on_rest_get(request)


@md.URL('user/device/location')
@md.URL('user/device/location/<int:pk>')
@md.requires_perms("manage_users", "manage_devices")
def on_user_device_location(request, pk=None):
    return UserDeviceLocation.on_rest_request(request, pk)


@md.URL('system/geoip')
@md.URL('system/geoip/<int:pk>')
@md.uses_model_security(GeoLocatedIP)
def on_geo_located_ip(request, pk=None):
    return GeoLocatedIP.on_rest_request(request, pk)


@md.GET('system/geoip/lookup')
@md.requires_params('ip')
@md.public_endpoint()
def on_geo_located_ip_lookup(request):
    ip_address = request.DATA.get('ip')
    auto_refresh = request.DATA.get('auto_refresh', True)
    geo_ip = GeoLocatedIP.geolocate(ip_address, auto_refresh=auto_refresh)
    return geo_ip.on_rest_get(request)


@md.URL('location/address/validate')
@md.requires_auth()
@md.requires_params("address1", "city", "state", "zip")
def on_address_validate(request, pk=None):
    from mojo.helprs.location.google import get_google_api
    google = get_google_api()
    return {"status": True, "data": google.validate_address(request.DATA)}

@md.URL('location/address/suggestions')
@md.requires_auth()
@md.requires_params("address")
def on_address_suggestions(request, pk=None):
    from mojo.helprs.location.google import get_google_api
    google = get_google_api()
    return {"status": True, "data": google.get_address_suggestions(request.DATA.address)}


@md.URL('location/address/geocode')
@md.requires_auth()
@md.requires_params("address")
def on_address_geocode_address(request, pk=None):
    from mojo.helprs.location.google import get_google_api
    google = get_google_api()
    return {
        "status": True,
        "data": google.geocode_address(request.DATA.address)
    }


@md.URL('location/geocode')
@md.requires_auth()
@md.requires_params("lat", "lng")
def on_address_geocode_reverse(request, pk=None):
    from mojo.helprs.location.google import get_google_api
    google = get_google_api()
    return {
        "status": True,
        "data": google.reverse_geocode(
            request.DATA.lat, request.DATA.lng)
    }


@md.URL('location/timezone')
@md.requires_auth()
@md.requires_params("lat", "lng")
def on_address_timezone(request, pk=None):
    from mojo.helprs.location.google import get_google_api
    google = get_google_api()
    return {
        "status": True,
        "data": google.get_timezone(
            request.DATA.lat, request.DATA.lng)
    }
