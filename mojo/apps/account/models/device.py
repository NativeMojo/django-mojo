import hashlib
from django.db import models
from mojo.helpers.settings import settings
from mojo.models import MojoModel
from mojo.helpers import dates, request as rhelper
from .geolocated_ip import GeoLocatedIP

GEOLOCATION_DEVICE_LOCATION_AGE = settings.get('GEOLOCATION_DEVICE_LOCATION_AGE', 300)



class UserDevice(models.Model, MojoModel):
    """
    Represents a unique device used by a user, tracked via a device ID (duid) or
    a hash of the user agent string as a fallback.
    """
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='devices')
    duid = models.CharField(max_length=255, db_index=True)

    device_info = models.JSONField(default=dict, blank=True)
    user_agent_hash = models.CharField(max_length=64, db_index=True, null=True, blank=True)

    last_ip = models.GenericIPAddressField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class RestMeta:
        VIEW_PERMS = ['manage_users', 'owner']
        GRAPHS = {
            'default': {
                'graphs': {
                    'user': 'basic'
                }
            },
            'basic': {
                "fields": ["duid", "last_ip", "last_seen", "device_info"],
            },
            'locations': {
                'fields': ['duid', 'last_ip', 'last_seen'],
                'graphs': {
                    'locations': 'default'
                }
            }
        }

    class Meta:
        unique_together = ('user', 'duid')
        ordering = ['-last_seen']

    def __str__(self):
        return f"Device {self.duid} for {self.user.username}"

    @classmethod
    def track(cls, request=None, user=None):
        """
        Tracks a user's device based on the incoming request. This is the primary
        entry point for the device tracking system.
        """
        if request is None:
            from mojo.models import rest
            request = rest.ACTIVE_REQUEST.get() if hasattr(rest.ACTIVE_REQUEST, "get") else rest.ACTIVE_REQUEST
            if request is None:
                raise ValueError("No active request found")

        if not user:
            user = request.user
        ip_address = request.ip
        user_agent_str = request.user_agent
        duid = request.duid

        ua_hash = hashlib.sha256(user_agent_str.encode('utf-8')).hexdigest()
        if not duid:
            duid = f"ua-hash-{ua_hash}"

        # Get or create the device
        device, created = cls.objects.get_or_create(
            user=user,
            duid=duid,
            defaults={
                'last_ip': ip_address,
                'user_agent_hash': ua_hash,
                'device_info': rhelper.parse_user_agent(user_agent_str)
            }
        )

        # If device already existed, update its last_seen and ip
        if not created:
            now = dates.utcnow()
            age_seconds = (now - device.last_seen).total_seconds()
            is_stale = age_seconds > GEOLOCATION_DEVICE_LOCATION_AGE
            if is_stale or device.last_ip != ip_address:
                device.last_ip = ip_address
                device.last_seen = dates.utcnow()
                # Optionally update device_info if user agent has changed
                if device.user_agent_hash != ua_hash:
                    device.user_agent_hash = ua_hash
                    device.device_info = rhelper.parse_user_agent(user_agent_str)
                device.save()

        # Track the location (IP) used by this device
        UserDeviceLocation.track(device, ip_address)

        return device


class UserDeviceLocation(models.Model, MojoModel):
    """
    A log linking a UserDevice to every IP address it uses. Geolocation is
    handled asynchronously.
    """
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='locations')
    user_device = models.ForeignKey('UserDevice', on_delete=models.CASCADE, related_name='locations')
    ip_address = models.GenericIPAddressField(db_index=True)
    geolocation = models.ForeignKey('GeoLocatedIP', on_delete=models.SET_NULL, null=True, blank=True, related_name='device_locations')

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class RestMeta:
        VIEW_PERMS = ['manage_users']
        GRAPHS = {
            'default': {
                'graphs': {
                    'user': 'basic',
                    'geolocation': 'default',
                    'user_device': 'basic'
                }
            },
            'list': {
                'graphs': {
                    'user': 'basic',
                    'geolocation': 'default',
                    'user_device': 'basic'
                }
            }
        }

    class Meta:
        unique_together = ('user', 'user_device', 'ip_address')
        ordering = ['-last_seen']

    def __str__(self):
        return f"{self.user_device} @ {self.ip_address}"

    @classmethod
    def track(cls, device, ip_address):
        """
        Creates or updates a device location entry, links it to a GeoLocatedIP record,
        and triggers a background refresh if the geo data is stale.
        """
        # First, get or create the geolocation record for this IP.
        # The actual fetching of data is handled by the background task.
        geo_ip = GeoLocatedIP.geolocate(ip_address)

        # Now, create the actual location event log, linking the device and the geo_ip record.
        location, loc_created = cls.objects.get_or_create(
            user=device.user,
            user_device=device,
            ip_address=ip_address,
            defaults={'geolocation': geo_ip}
        )

        if not loc_created:
            now = dates.utcnow()
            age_seconds = (now - location.last_seen).total_seconds()
            if age_seconds > GEOLOCATION_DEVICE_LOCATION_AGE:
                location.last_seen = now
                # If the location already existed but wasn't linked to a geo_ip object yet
                if not location.geolocation:
                    location.geolocation = geo_ip
                location.save(update_fields=['last_seen', 'geolocation'])

        # Finally, if the geo data is stale or new, refresh it.
        # TODO: Add optional async job execution
        if geo_ip.is_expired:
            geo_ip.refresh()

        return location
