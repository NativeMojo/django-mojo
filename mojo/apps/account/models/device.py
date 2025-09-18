import hashlib
from django.db import models
from mojo.helpers.settings import settings
from mojo.models import MojoModel
from mojo.helpers import dates, request as rhelper
from mojo.apps import jobs
from mojo.helpers.location.geolocation import refresh_geolocation_for_ip

GEOLOCATION_ALLOW_SUBNET_LOOKUP = settings.get('GEOLOCATION_ALLOW_SUBNET_LOOKUP', False)
GEOLOCATION_DEVICE_LOCATION_AGE = settings.get('GEOLOCATION_DEVICE_LOCATION_AGE', 300)
GEOLOCATION_CACHE_DURATION_DAYS = settings.get('GEOLOCATION_CACHE_DURATION_DAYS', 30)


def trigger_refresh_task(ip_address):
    """
    Publishes a task to refresh the geolocation data for a given IP address.
    """
    jobs.publish_local(refresh_geolocation_for_ip, ip_address)


class GeoLocatedIP(models.Model, MojoModel):
    """
    Acts as a cache to store geolocation results, reducing redundant and costly API calls.
    Features a standardized, indexed schema for fast querying.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    ip_address = models.GenericIPAddressField(db_index=True, unique=True)
    subnet = models.CharField(max_length=16, db_index=True, null=True, default=None)

    # Normalized and indexed fields for querying
    country_code = models.CharField(max_length=3, db_index=True, null=True, blank=True)
    country_name = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=50, null=True, blank=True)

    # Auditing and source tracking
    provider = models.CharField(max_length=50, null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(default=None, null=True, blank=True)

    class Meta:
        verbose_name = "Geolocated IP"
        verbose_name_plural = "Geolocated IPs"

    class RestMeta:
        VIEW_PERMS = ['manage_users']
        GRAPHS = {
            'default': {

            }
        }

    def __str__(self):
        return f"{self.ip_address} ({self.city}, {self.country_code})"

    @property
    def is_expired(self):
        if self.provider == 'internal':
            return False # Internal records never expire
        if self.expires_at:
            return dates.utcnow() > self.expires_at
        return True # If no expiry is set, it needs a refresh

    def refresh(self):
        """
        Refreshes the geolocation data for this IP by calling the geolocation
        helper and updating the model instance with the returned data.
        """
        from mojo.helpers.location import geolocation
        from datetime import timedelta

        geo_data = geolocation.geolocate_ip(self.ip_address)

        if not geo_data:
            return False

        # Update self with new data
        for key, value in geo_data.items():
            setattr(self, key, value)

        # Set the expiration date
        if self.provider == 'internal':
            self.expires_at = None
        else:
            cache_duration_days = GEOLOCATION_CACHE_DURATION_DAYS
            self.expires_at = dates.utcnow() + timedelta(days=cache_duration_days)

        self.save()
        return True

    @classmethod
    def geolocate(cls, ip_address, auto_refresh=False, subdomain_only=False):
        # Extract subnet from IP address using simple string parsing
        subnet = ip_address[:ip_address.rfind('.')]
        geo_ip = GeoLocatedIP.objects.filter(ip_address=ip_address).first()
        if not geo_ip and (GEOLOCATION_ALLOW_SUBNET_LOOKUP or subdomain_only):
            geo_ip = GeoLocatedIP.objects.filter(subnet=subnet).last()
            if geo_ip:
                geo_ip.id = None
                geo_ip.pk = None
                geo_ip.ip_address = ip_address
                if "subnet" not in geo_ip.provider:
                    geo_ip.provider = f"subnet:{geo_ip.provider}"
        if not geo_ip:
            geo_ip = GeoLocatedIP.objects.create(ip_address=ip_address, subnet=subnet)
        if auto_refresh and geo_ip.is_expired:
            geo_ip.refresh()
        return geo_ip



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
        if not request:
            request = self.active_request
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
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='device_locations_direct')
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

        # Finally, if the geo data is stale or new, trigger a refresh.
        if geo_ip.is_expired:
            trigger_refresh_task(ip_address)

        return location
