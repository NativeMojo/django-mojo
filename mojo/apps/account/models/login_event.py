from django.db import models
from mojo.models import MojoModel
from mojo.helpers.settings import settings
from mojo.helpers import dates, request as rhelper
from mojo.apps import metrics


LOGIN_EVENT_TRACKING_ENABLED = settings.get_static('LOGIN_EVENT_TRACKING_ENABLED', True, kind='bool')
LOGIN_EVENT_FLAG_NEW_COUNTRY = settings.get_static('LOGIN_EVENT_FLAG_NEW_COUNTRY', True, kind='bool')
LOGIN_EVENT_FLAG_NEW_REGION = settings.get_static('LOGIN_EVENT_FLAG_NEW_REGION', True, kind='bool')


class UserLoginEvent(models.Model, MojoModel):
    """
    One row per successful login. Denormalizes geo data from GeoLocatedIP
    for fast filtering and aggregation without joins.
    """
    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='login_events')
    device = models.ForeignKey("account.UserDevice", on_delete=models.SET_NULL, null=True, blank=True, related_name='login_events')

    ip_address = models.GenericIPAddressField(db_index=True)
    country_code = models.CharField(max_length=3, db_index=True, null=True, blank=True)
    region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    source = models.CharField(max_length=32, db_index=True, default='', blank=True)
    user_agent_info = models.JSONField(default=dict, blank=True)

    is_new_country = models.BooleanField(default=False, db_index=True)
    is_new_region = models.BooleanField(default=False, db_index=True)

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ['-created']
        indexes = [
            models.Index(fields=['user', 'country_code']),
            models.Index(fields=['user', 'country_code', 'region']),
        ]

    class RestMeta:
        CAN_CREATE = False
        CAN_SAVE = False
        CAN_DELETE = False
        VIEW_PERMS = ['manage_users', 'security', 'users']
        SEARCH_FIELDS = ['ip_address', 'country_code', 'region', 'city']
        GRAPHS = {
            'list': {
                'fields': [
                    'id', 'ip_address', 'country_code', 'region', 'city',
                    'latitude', 'longitude', 'source',
                    'is_new_country', 'is_new_region', 'created',
                ],
                'graphs': {
                    'user': 'basic',
                },
            },
            'default': {
                'fields': [
                    'id', 'ip_address', 'country_code', 'region', 'city',
                    'latitude', 'longitude', 'source', 'user_agent_info',
                    'is_new_country', 'is_new_region', 'created', 'modified',
                ],
                'graphs': {
                    'user': 'basic',
                    'device': 'basic',
                },
            },
        }

    def __str__(self):
        return f"Login {self.user_id} from {self.ip_address} ({self.country_code or '?'})"

    @classmethod
    def track(cls, request, user, device=None, source=None):
        if not LOGIN_EVENT_TRACKING_ENABLED:
            return None

        from .geolocated_ip import GeoLocatedIP
        geo = GeoLocatedIP.objects.filter(ip_address=request.ip).first()

        country_code = None
        region = None
        city = None
        latitude = None
        longitude = None

        if geo:
            country_code = geo.country_code
            region = geo.region
            city = geo.city
            latitude = geo.latitude
            longitude = geo.longitude

        is_new_country = False
        is_new_region = False

        if country_code and LOGIN_EVENT_FLAG_NEW_COUNTRY:
            is_new_country = not cls.objects.filter(
                user=user, country_code=country_code
            ).exists()

        if country_code and region and LOGIN_EVENT_FLAG_NEW_REGION:
            is_new_region = not cls.objects.filter(
                user=user, country_code=country_code, region=region
            ).exists()

        ua_info = rhelper.parse_user_agent(request.user_agent) if request.user_agent else {}

        event = cls.objects.create(
            user=user,
            device=device,
            ip_address=request.ip,
            country_code=country_code,
            region=region,
            city=city,
            latitude=latitude,
            longitude=longitude,
            source=source or '',
            user_agent_info=ua_info,
            is_new_country=is_new_country,
            is_new_region=is_new_region,
        )

        # Record metrics
        if country_code:
            metrics.record(f"login:country:{country_code}", category="logins")
            if region:
                metrics.record(f"login:region:{country_code}:{region}", category="logins")
        if is_new_country:
            metrics.record("login:new_country", category="logins")
        if is_new_region:
            metrics.record("login:new_region", category="logins")

        return event
