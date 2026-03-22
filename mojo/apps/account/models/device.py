import hashlib
from django.db import models
from mojo.helpers.settings import settings
from mojo.models import MojoModel
from mojo.helpers import dates, request as rhelper
from .geolocated_ip import GeoLocatedIP

GEOLOCATION_DEVICE_LOCATION_AGE = settings.get_static('GEOLOCATION_DEVICE_LOCATION_AGE', 300)



class UserDevice(models.Model, MojoModel):
    """
    Represents a unique device used by an authenticated user.

    `muid` is the server-controlled device identity (HttpOnly cookie _muid),
    linking this post-auth record to the pre-auth BouncerDevice. This allows
    admins to see the full pre-auth bouncer history for any user's device.

    `duid` is the client-claimed device ID from localStorage (kept for backward
    compat).
    """
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='devices')
    muid = models.CharField(max_length=64, db_index=True, default='', blank=True)
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
                "fields": ["muid", "duid", "last_ip", "last_seen", "device_info"],
            },
            'locations': {
                'fields': ['muid', 'duid', 'last_ip', 'last_seen'],
                'graphs': {
                    'locations': 'default'
                }
            },
            'sessions': {
                'fields': [
                    'id', 'muid', 'duid', 'last_ip', 'device_info',
                    'first_seen', 'last_seen',
                ],
                'extra': ['bouncer_device', 'active_sessions', 'recent_locations'],
                'graphs': {
                    'user': 'basic',
                },
            },
        }

    class Meta:
        unique_together = ('user', 'duid')
        ordering = ['-last_seen']

    def __str__(self):
        return f"Device {self.duid} for {self.user.username}"

    @property
    def bouncer_device(self):
        """Return BouncerDevice data for this muid (pre-auth reputation)."""
        if not self.muid:
            return None
        from .bouncer_device import BouncerDevice
        bd = BouncerDevice.objects.filter(muid=self.muid).first()
        if not bd:
            return None
        return {
            'risk_tier': bd.risk_tier,
            'event_count': bd.event_count,
            'block_count': bd.block_count,
            'fingerprint_id': bd.fingerprint_id or '',
            'first_seen': bd.first_seen,
            'last_seen': bd.last_seen,
        }

    @property
    def active_sessions(self):
        """Return recent browser sessions grouped by msid from BouncerSignal."""
        if not self.muid:
            return []
        from .bouncer_signal import BouncerSignal
        from django.db.models import Min, Max, Count
        from mojo.helpers import dates
        from datetime import timedelta
        cutoff = dates.utcnow() - timedelta(hours=24)
        qs = BouncerSignal.objects.filter(
            muid=self.muid, created__gte=cutoff
        ).exclude(msid='').values('msid').annotate(
            started=Min('created'),
            last_activity=Max('created'),
            ip=Max('ip_address'),
            signal_count=Count('id'),
        ).order_by('-last_activity')[:20]
        sessions = []
        for row in qs:
            tabs = list(BouncerSignal.objects.filter(
                muid=self.muid, msid=row['msid'], created__gte=cutoff
            ).exclude(mtab='').values('mtab').annotate(
                started=Min('created'),
                last_activity=Max('created'),
                signal_count=Count('id'),
            ).order_by('-last_activity')[:10])
            sessions.append({
                'msid': row['msid'],
                'started': row['started'],
                'last_activity': row['last_activity'],
                'ip': row['ip'],
                'signal_count': row['signal_count'],
                'tabs': tabs,
            })
        return sessions

    @property
    def recent_locations(self):
        """Return recent locations for this device."""
        locs = self.locations.select_related('geolocation').order_by('-last_seen')[:10]
        result = []
        for loc in locs:
            entry = {
                'ip_address': loc.ip_address,
                'first_seen': loc.first_seen,
                'last_seen': loc.last_seen,
            }
            if loc.geolocation:
                entry['city'] = loc.geolocation.city or ''
                entry['country'] = loc.geolocation.country_code or ''
            result.append(entry)
        return result

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
        muid = getattr(request, 'muid', None)
        muid = muid if isinstance(muid, str) else ''

        ua_hash = hashlib.sha256(user_agent_str.encode('utf-8')).hexdigest()
        if not duid:
            duid = f"ua-hash-{ua_hash}"

        # Get or create the device
        device, created = cls.objects.get_or_create(
            user=user,
            duid=duid,
            defaults={
                'muid': muid,
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
            update_fields = []
            if is_stale or device.last_ip != ip_address:
                device.last_ip = ip_address
                device.last_seen = dates.utcnow()
                update_fields.extend(['last_ip', 'last_seen'])
                if device.user_agent_hash != ua_hash:
                    device.user_agent_hash = ua_hash
                    device.device_info = rhelper.parse_user_agent(user_agent_str)
                    update_fields.extend(['user_agent_hash', 'device_info'])
            # Always update muid if we have one and the device doesn't yet
            if muid and device.muid != muid:
                device.muid = muid
                update_fields.append('muid')
            if update_fields:
                device.save(update_fields=update_fields)

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
