from mojo import decorators as md
from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
from mojo.apps.account.models.geolocated_ip import GeoLocatedIP


@md.URL('user/device')
@md.URL('user/device/<int:pk>')
def on_user_device(request, pk=None):
    return UserDevice.on_rest_request(request, pk)


@md.GET('user/device/lookup')
@md.requires_params('duid')
@md.requires_perms("manage_users", "manage_devices", "users")
def on_user_device_by_duid(request):
    duid = request.DATA.get('duid')
    device = UserDevice.objects.filter(duid=duid).first()
    if not device:
        return UserDevice.rest_error_response(request, 404, error="Device not found")
    return device.on_rest_get(request)


@md.URL('user/device/location')
@md.URL('user/device/location/<int:pk>')
@md.requires_perms("manage_users", "manage_devices", "users")
def on_user_device_location(request, pk=None):
    return UserDeviceLocation.on_rest_request(request, pk)


@md.URL('system/geoip')
@md.URL('system/geoip/<int:pk>')
@md.uses_model_security(GeoLocatedIP)
def on_geo_located_ip(request, pk=None):
    return GeoLocatedIP.on_rest_request(request, pk)


@md.GET('system/geoip/lookup')
@md.requires_params('ip')
@md.rate_limit("geoip_lookup", ip_limit=30)
@md.requires_auth()
def on_geo_located_ip_lookup(request):
    ip_address = request.DATA.get('ip')
    auto_refresh = request.DATA.get('auto_refresh', True)
    geo_ip = GeoLocatedIP.geolocate(ip_address, auto_refresh=auto_refresh)
    return geo_ip.on_rest_get(request)


@md.GET('system/geoip/time')
@md.rate_limit("geoip_time", ip_limit=30)
@md.public_endpoint()
def on_geo_located_ip_time(request):
    from mojo.helpers import dates
    geo_ip = GeoLocatedIP.geolocate(request.ip)
    timezone = geo_ip.timezone
    if not timezone:
        return {"status": False, "error": "Timezone not available for this IP"}
    local_time = dates.get_local_time(timezone)
    return {
        "status": True,
        "data": {
            "ip": request.ip,
            "timezone": timezone,
            "epoch": int(local_time.timestamp()),
            "iso": local_time.isoformat()
        }
    }


# Per-fleet enforcement fields that are never federated. Reject any inbound
# sync payload that tries to set them.
_GEOIP_SYNC_FORBIDDEN_FIELDS = (
    "is_blocked", "is_whitelisted",
    "blocked_at", "blocked_until", "blocked_reason", "block_count",
    "whitelisted_reason", "whitelisted_until",
)


@md.POST('system/geoip/sync')
@md.rate_limit("geoip_sync", ip_limit=60)
@md.requires_params('ip')
@md.requires_perms('geoip_sync')
def on_geo_located_ip_sync(request):
    """
    Receive abuse-signal updates from a downstream mojo instance.

    Payload: {ip, threat_level?, is_known_attacker?, is_known_abuser?}
    Any subset of the three signal fields is allowed; at least one must
    be present.

    Apply semantics:
      - threat_level: MAX (never downgrade).
      - is_known_attacker / is_known_abuser: OR (only flip False -> True).

    Per-fleet enforcement state (is_blocked, is_whitelisted, blocked_*,
    whitelisted_*) is explicitly rejected — that state is not federated.

    Loop prevention: the endpoint applies via raw save(update_fields=...)
    rather than block()/check_threats()/update_threat_from_incident(), so
    the _maybe_push_abuse_signals hook never fires on the receiver.
    """
    # Reject any per-fleet enforcement fields up-front.
    for forbidden in _GEOIP_SYNC_FORBIDDEN_FIELDS:
        if forbidden in request.DATA:
            return {"status": False, "error": f"Field '{forbidden}' is not federated"}

    ip = request.DATA.get('ip')
    incoming_level = request.DATA.get('threat_level', None)
    incoming_attacker = request.DATA.get('is_known_attacker', None)
    incoming_abuser = request.DATA.get('is_known_abuser', None)

    if incoming_level is None and incoming_attacker is None and incoming_abuser is None:
        return {"status": False, "error": "At least one signal field is required"}

    if incoming_level is not None and incoming_level not in ('low', 'medium', 'high', 'critical'):
        return {"status": False, "error": "Invalid threat_level"}

    geo = GeoLocatedIP.geolocate(ip, auto_refresh=False)
    update_fields = []
    applied = {}

    if incoming_level is not None:
        order = GeoLocatedIP.THREAT_LEVEL_ORDER
        prev_idx = order.index(geo.threat_level) if geo.threat_level in order else 0
        new_idx = order.index(incoming_level)
        if new_idx > prev_idx:
            geo.threat_level = incoming_level
            update_fields.append('threat_level')
            applied['threat_level'] = incoming_level

    # OR semantics for boolean abuse flags — never flip True -> False.
    if incoming_attacker is True and not geo.is_known_attacker:
        geo.is_known_attacker = True
        update_fields.append('is_known_attacker')
        applied['is_known_attacker'] = True

    if incoming_abuser is True and not geo.is_known_abuser:
        geo.is_known_abuser = True
        update_fields.append('is_known_abuser')
        applied['is_known_abuser'] = True

    if update_fields:
        # Direct save — does NOT call block()/check_threats(), so no outbound
        # push fires (loop prevention).
        geo.save(update_fields=update_fields)

    return {"status": True, "data": {
        "ip": ip,
        "threat_level": geo.threat_level,
        "is_known_attacker": geo.is_known_attacker,
        "is_known_abuser": geo.is_known_abuser,
        "applied": applied,
    }}


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
