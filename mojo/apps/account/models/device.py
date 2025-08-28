import hashlib
from django.db import models
from django.conf import settings
from mojo.models import MojoModel
from mojo.helpers import dates, request as rhelper

# Placeholder for a background task runner
# In a real implementation, you would replace this with your project's task runner (e.g., Celery)
def trigger_geolocation_task(ip_address):
    print(f"BG Task: Geolocation needed for {ip_address}")
    # from mojo.helpers.location.geolocation import geolocate
    # geolocate.delay(ip_address) # Example for Celery


class GeoLocatedIP(models.Model, MojoModel):
    """
    Acts as a cache to store geolocation results, reducing redundant and costly API calls.
    Features a standardized, indexed schema for fast querying.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    ip_address = models.GenericIPAddressField(db_index=True, unique=True)

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

    def __str__(self):
        return f"{self.ip_address} ({self.city}, {self.country_code})"

    @property
    def is_expired(self):
        if self.expires_at:
            return dates.utcnow() > self.expires_at
        return False


class UserDevice(models.Model, MojoModel):
    """
    Represents a unique device used by a user, tracked via a device ID (duid) or
    a hash of the user agent string as a fallback.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='devices')
    duid = models.CharField(max_length=255, db_index=True)

    device_info = models.JSONField(default=dict, blank=True)
    user_agent_hash = models.CharField(max_length=64, db_index=True, null=True, blank=True)

    last_ip = models.GenericIPAddressField(null=True, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'duid')
        ordering = ['-last_seen']

    def __str__(self):
        return f"Device {self.duid} for {self.user.username}"

    @classmethod
    def track(cls, request):
        """
        Tracks a user's device based on the incoming request. This is the primary
        entry point for the device tracking system.
        """
        if not request.user or not request.user.is_authenticated:
            return None

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


class UserDeviceLocation(MojoModel):
    """
    A log linking a UserDevice to every IP address it uses. Geolocation is
    handled asynchronously.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='device_locations_direct')
    user_device = models.ForeignKey(UserDevice, on_delete=models.CASCADE, related_name='locations')
    ip_address = models.GenericIPAddressField(db_index=True)
    geolocation = models.ForeignKey(GeoLocatedIP, on_delete=models.SET_NULL, null=True, blank=True, related_name='device_locations')

    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'user_device', 'ip_address')
        ordering = ['-last_seen']

    def __str__(self):
        return f"{self.user_device} @ {self.ip_address}"

    @classmethod
    def track(cls, device, ip_address):
        """
        Creates or updates a device location entry and triggers geolocation
        if the IP is unknown or its cache has expired.
        """
        location, created = cls.objects.get_or_create(
            user=device.user,
            user_device=device,
            ip_address=ip_address
        )

        if not created:
            location.last_seen = dates.utcnow()
            location.save(update_fields=['last_seen'])

        # Check if geolocation is needed
        if not location.geolocation or location.geolocation.is_expired:
            # Check if a valid cache entry already exists for this IP
            existing_geo = GeoLocatedIP.objects.filter(ip_address=ip_address).first()
            if existing_geo and not existing_geo.is_expired:
                if location.geolocation != existing_geo:
                    location.geolocation = existing_geo
                    location.save(update_fields=['geolocation'])
            else:
                # Trigger background task to fetch geolocation data
                trigger_geolocation_task(ip_address)

        return location
